package auth

import (
	"errors"
	"fmt"
)

type User struct {
	ID    int
	Name  string
	Email string
}

type UserRepository interface {
	FindByID(id int) (*User, error)
	Save(user *User) error
}

type InMemoryRepo struct {
	users map[int]*User
}

func NewInMemoryRepo() *InMemoryRepo {
	return &InMemoryRepo{users: make(map[int]*User)}
}

func (r *InMemoryRepo) FindByID(id int) (*User, error) {
	user, ok := r.users[id]
	if !ok {
		return nil, errors.New("user not found")
	}
	return user, nil
}

func (r *InMemoryRepo) Save(user *User) error {
	r.users[user.ID] = user
	fmt.Printf("Saved user %d\n", user.ID)
	return nil
}

func (r *InMemoryRepo) SaveAndReturn(user *User) error {
	return (*r).Save(user)
}

type ShadowA struct{}
type ShadowB struct{}
type ShadowC int

func (a *ShadowA) Save() bool { return true }
func (b *ShadowB) Save() bool { return true }
func (c ShadowC) Save() bool  { return true }

func (a *ShadowA) CallsShadowedReceiver() {
	func(a *ShadowB) { a.Save() }(&ShadowB{})
}

func (a *ShadowA) CallsBlockShadowedReceiver() {
	if true {
		a := &ShadowB{}
		a.Save()
	}
}

func (a *ShadowA) CallsVarShadowedReceiver() {
	var a *ShadowB
	a.Save()
}

func (a *ShadowA) CallsRangeShadowedReceiver() {
	for a := range []int{1} {
		a.Save()
	}
}

func (a *ShadowA) CallsForClauseShadowedReceiver() {
	for a := &ShadowB{}; a.Save(); a.Save() {
		a.Save()
		break
	}
}

func (a *ShadowA) CallsTypeSwitchShadowedReceiver(value any) {
	switch a := value.(type) {
	case *ShadowB:
		a.Save()
	}
}

func (a *ShadowA) CallsExpressionCaseShadowedReceiver() {
	switch 1 {
	case 1:
		var a *ShadowB
		a.Save()
	}
}

func (a *ShadowA) CallsSelectCaseShadowedReceiver(ch <-chan struct{}) {
	select {
	case <-ch:
		var a *ShadowB
		a.Save()
	default:
	}
}

func (a *ShadowA) CallsNamedResultShadowedReceiver() {
	func() (a *ShadowB) {
		a.Save()
		return nil
	}()
}

func (a *ShadowA) CallsAfterShadowScope() {
	{
		var a *ShadowB
		a.Save()
	}
	a.Save()
}

func (a *ShadowA) CallsInitializerScope() {
	if a := func() *ShadowB {
		a.Save()
		return nil
	}(); a != nil {
		a.Save()
	}
}

func (a *ShadowA) CallsSameScopeRedeclaration() {
	a, n := a, 1
	_ = n
	a.Save()
}

func (a *ShadowA) CallsTypeSwitchInitShadowedReceiver(value any) {
	switch a := func() *ShadowB {
		a.Save()
		return value.(*ShadowB)
	}(); value := any(a).(type) {
	case *ShadowB:
		_ = value
		a.Save()
	}
	a.Save()
}

func (a *ShadowA) CallsSelectReceiveShadowedReceiver(ch <-chan *ShadowB) {
	select {
	case a := <-func() <-chan *ShadowB {
		a.Save()
		return ch
	}():
		a.Save()
	default:
	}
	a.Save()
}

func (a *ShadowA) CallsConstShadowedReceiver() {
	{
		const a ShadowC = 0
		a.Save()
	}
	a.Save()
}

func (a *ShadowA) CallsTypeShadowedReceiver() {
	{
		type a = ShadowC
		a.Save(0)
	}
	a.Save()
}

func CreateUser(repo UserRepository, name string, email string) (*User, error) {
	user := &User{ID: 1, Name: name, Email: email}
	err := repo.Save(user)
	if err != nil {
		return nil, err
	}
	return user, nil
}
